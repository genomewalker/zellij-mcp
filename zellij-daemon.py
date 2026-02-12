#!/usr/bin/env python3
"""
Zellij Daemon - Runs INSIDE a Zellij session to provide pane reading.

Since it runs attached to the session, dump-screen works correctly.
Listens on a Unix socket for commands from the MCP server.

Usage:
    # Start daemon (typically in a hidden/background pane)
    python zellij-daemon.py --socket /tmp/zellij-daemon-<session>.sock

    # MCP server sends requests to the socket
"""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time


class ZellijDaemon:
    """Daemon that runs inside Zellij to provide pane operations."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.running = False
        self.lock = threading.Lock()
        self.session = os.environ.get("ZELLIJ_SESSION_NAME", "")
        self.plugin_path = os.path.expanduser(
            "~/.local/share/zellij-mcp/plugins/zellij-pane-bridge.wasm"
        )

    def start(self):
        """Start the daemon."""
        if not self.session:
            print("ERROR: Not running inside a Zellij session", file=sys.stderr)
            sys.exit(1)

        # Clean up old socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(5)
        server.settimeout(1.0)

        self.running = True
        print(f"Zellij daemon running in session: {self.session}", file=sys.stderr)
        print(f"Listening on: {self.socket_path}", file=sys.stderr)

        try:
            while self.running:
                try:
                    conn, _ = server.accept()
                    threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
        finally:
            server.close()
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)

    def _handle_client(self, conn):
        """Handle a client connection."""
        try:
            data = conn.recv(8192).decode('utf-8').strip()
            if not data:
                return

            request = json.loads(data)
            cmd = request.get('cmd', '')

            if cmd == 'read':
                pane_id = request.get('pane_id')
                full = request.get('full', False)
                tail = request.get('tail')
                response = self._read_pane(pane_id, full, tail)
            elif cmd == 'focus':
                pane_id = request.get('pane_id')
                response = self._focus_pane(pane_id)
            elif cmd == 'write':
                pane_id = request.get('pane_id')
                chars = request.get('chars', '')
                response = self._write_pane(pane_id, chars)
            elif cmd == 'list':
                response = self._list_panes()
            elif cmd == 'status':
                response = {'success': True, 'session': self.session, 'pid': os.getpid()}
            elif cmd == 'stop':
                self.running = False
                response = {'success': True, 'message': 'Stopping daemon'}
            else:
                response = {'success': False, 'error': f'Unknown command: {cmd}'}

            conn.send(json.dumps(response).encode('utf-8'))
        except Exception as e:
            try:
                conn.send(json.dumps({'success': False, 'error': str(e)}).encode('utf-8'))
            except:
                pass
        finally:
            conn.close()

    def _plugin_cmd(self, cmd: str, payload: dict = None) -> dict:
        """Execute a plugin command."""
        if not os.path.exists(self.plugin_path):
            return {'success': False, 'error': 'Plugin not found'}

        payload_json = json.dumps(payload) if payload else '{}'
        try:
            result = subprocess.run(
                ['timeout', '5', 'zellij', 'pipe',
                 '-p', f'file://{self.plugin_path}', '-n', cmd, '--', payload_json],
                capture_output=True, text=True, timeout=7
            )
            stdout = result.stdout.strip()
            if stdout:
                # Handle duplicate JSON responses
                if '}{' in stdout:
                    stdout = stdout.split('}{')[0] + '}'
                return json.loads(stdout)
            return {'success': False, 'error': 'No output'}
        except json.JSONDecodeError as e:
            return {'success': False, 'error': f'JSON decode error: {e}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _list_panes(self) -> dict:
        """List all panes via plugin."""
        return self._plugin_cmd('list')

    def _focus_pane(self, pane_id: int) -> dict:
        """Focus a pane by ID."""
        return self._plugin_cmd('focus', {'pane_id': pane_id})

    def _write_pane(self, pane_id: int, chars: str) -> dict:
        """Write to a pane by ID."""
        return self._plugin_cmd('write', {'pane_id': pane_id, 'chars': chars})

    def _dump_screen(self, full: bool = False) -> str:
        """Dump current screen content."""
        import tempfile
        # Must use temp file - /dev/stdout doesn't work with capture_output
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            tmp_path = f.name
        try:
            args = ['zellij', 'action', 'dump-screen', tmp_path]
            if full:
                args.append('--full')
            subprocess.run(args, timeout=5)
            with open(tmp_path, 'r', errors='replace') as f:
                return f.read()
        except Exception:
            return ''
        finally:
            try:
                os.unlink(tmp_path)
            except:
                pass

    def _strip_ansi(self, text: str) -> str:
        """Remove ANSI escape codes."""
        ansi_escape = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07')
        return ansi_escape.sub('', text)

    def _read_pane(self, pane_id: int, full: bool = False, tail: int = None) -> dict:
        """Read content from a specific pane."""
        with self.lock:
            # Save current focus to restore later
            list_result = self._list_panes()
            original_focused = None
            if list_result.get('success'):
                for pane in list_result.get('data', []):
                    if pane.get('is_focused') and not pane.get('is_plugin'):
                        original_focused = pane.get('id')
                        break

            # Focus the target pane
            focus_result = self._focus_pane(pane_id)
            if not focus_result.get('success'):
                return focus_result

            # Small delay for focus to take effect
            time.sleep(0.1)

            # Dump screen content (works because we're attached!)
            content = self._dump_screen(full)
            content = self._strip_ansi(content)

            # Apply tail if requested
            if tail and tail > 0:
                lines = content.split('\n')
                content = '\n'.join(lines[-tail:])

            # Restore original focus
            if original_focused is not None and original_focused != pane_id:
                self._focus_pane(original_focused)

            return {'success': True, 'content': content, 'pane_id': pane_id}

    def stop(self):
        """Stop the daemon."""
        self.running = False


def main():
    parser = argparse.ArgumentParser(description='Zellij Daemon')
    parser.add_argument('--socket', '-s', help='Unix socket path')

    args = parser.parse_args()

    # Default socket path based on session
    session = os.environ.get("ZELLIJ_SESSION_NAME", "default")
    socket_path = args.socket or f"/tmp/zellij-daemon-{session}.sock"

    daemon = ZellijDaemon(socket_path)

    def signal_handler(sig, frame):
        print("\nStopping daemon...", file=sys.stderr)
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    daemon.start()


if __name__ == '__main__':
    main()
